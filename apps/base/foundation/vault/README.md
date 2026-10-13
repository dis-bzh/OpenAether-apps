# OpenBao (foundation-vault) — architecture and runbook

`apps/base/foundation/vault/`. One OpenBao per cluster: 3 raft replicas, TLS from cert-manager, **static auto-unseal**, an
audit trail, **no root token and no unseal key kept in the cluster**. It stays out of the Istio mesh (Cilium network
policies at L3/L4 + OpenBao's own authentication).

## How it fits together

```
 StatefulSet openbao (3 replicas, raft, local-path-retain)
   seal "static"  ← Secret openbao-seal-key (32 random bytes, mounted read-only)   → restarts unseal themselves
   audit "file"   → stdout → Alloy → Loki (values are HMAC-ed by OpenBao)          → declared in the config, not via the API
 Job openbao-init       one shot; the ONLY holder of the root token, for a few seconds
 Job openbao-vault-bootstrap   idempotent; runs as the Kubernetes-auth identity `openbao-bootstrap` (15 min tokens)
 ConfigMap openbao-bootstrap-scripts   scripts/{lib,init,configure}.sh + scripts/policies/*.hcl (the single source of truth)
```

**Bootstrap, in order** (every step is idempotent, a re-run on a configured cluster changes nothing):

1. `init.sh` creates the Secret `openbao-seal-key` **once** (`kubectl create`, never `apply`: an existing key is never overwritten)
   and the pods start.
2. `sys/init` with *recovery* shares. The root token lives in a file on a memory-backed volume. With it: the bootstrap and
   break-glass policies, the Kubernetes auth method (no CA pinned: it uses the OpenBao pod's own, so a snapshot restored onto a
   rebuilt cluster still logs in), the roles `openbao-bootstrap` and `openbao-breakglass`. It then checks that the bootstrap role can
   log in (from the Job's own ServiceAccount) **and revokes the root token**, also when anything in the window fails or the Job is killed
   (a token left by a killed run is revoked at the next start). Before `sys/init` it refuses what would leave a half-initialised cluster:
   a bad threshold, a key that is not base64, a leftover shares Secret. From here on nothing in the cluster holds a root token.
3. `configure.sh` logs in as `openbao-bootstrap` and creates the engines (`pki`, KV v2 `secret/`), the 8 policies, the
   Kubernetes-auth roles and the application secrets (random, **create-only**: `cas=0`, so a re-run can not rotate one).
   Nothing secret is ever printed: pod logs are shipped to Loki and kept.

| Kubernetes-auth role | ServiceAccount (ns) | Policy | Can |
|---|---|---|---|
| `openbao-bootstrap` | `openbao-vault-bootstrap`, `openbao-init` | `openaether-bootstrap` | configure engines/auth/policies/secrets. **Root-equivalent in practice** (it writes policies and roles, so it can grant itself anything); the control is who may run as those ServiceAccounts |
| `external-secrets-operator` | `external-secrets` (foundation-pki-management) | `external-secrets-read` | read `secret/` |
| `cert-manager-issuer` | `cert-manager` | `cert-manager-pki` | sign with `pki/sign/*` |
| `backup-snapshot` | `openbao-snapshot` | `backup-snapshot` | read the raft snapshot |
| `secret-seeder` | `openbao-seeder` | `secret-seeder` | **create** the Day-1 secrets listed in the policy; never overwrite, never read a value |
| `openaether-admin` | `openaether-admin` | `openaether-admin` | read/write secrets, import the signed intermediate, issue from the `openaether` PKI role, leases; read the configuration. **Not** write policies/roles/PKI roles/mounts (those change through git), mint tokens or touch the seal |
| `openaether-reader` | `openaether-reader` | `openaether-reader` | read secrets |
| `openbao-breakglass` | `openbao-breakglass` | `openaether-breakglass` | start a root-token generation (still needs the recovery quorum) |

No Pod runs as `openbao-seeder`, `openaether-admin`, `openaether-reader` or `openbao-breakglass`: a cluster administrator mints a
short token with `kubectl -n foundation-vault create token <sa> --duration=15m` when one is needed.

## Production or development

| | Production | Development |
|---|---|---|
| Selected by | the ConfigMap `openbao-operator-keys` exists | it does not |
| Recovery shares | N, threshold T, each **encrypted to one operator's PGP key** → Secret `openbao-recovery-shares` | 1 plaintext share → Secret `openbao-recovery-dev` (labelled `openaether.io/dev-bootstrap=true`) |

The ConfigMap holds `pgp-1 … pgp-N` (each an operator's *public* key, `gpg --export <id> | base64 -w0`; N ≥ 3) and optionally
`threshold` (default ⌊N/2⌋+1, at least 2). Create it **before** the first reconcile of this brick (`OpenAether-infra` does,
from `openbao_recovery_pgp_keys`). The shares in the Secret are useless without the operators' private keys, so they can be copied
anywhere. A development cluster works and says it is not production; do not put anything that matters on one, and know that
**it can not be turned into a production one**: re-initialise it. Check every key before you paste it: `gpg --list-keys <id>` must
show an `[E]` (a signing-only or expired key makes `sys/init` fail *after* it has initialised the cluster: re-init).

## Day 0 — what an operator must do once (a production cluster)

The cluster can not do these for you, and **losing the seal key loses every secret**:

```bash
# 1. Escrow the seal key, encrypted, off the cluster. (It is the master key of the raft data.)
kubectl -n foundation-vault get secret openbao-seal-key -o jsonpath='{.data.key}' | base64 -d \
  | gpg --encrypt --recipient <ops-key> --output openbao-seal-key.gpg
# 2. Keep a copy of the (already PGP-encrypted) recovery shares: the Secret dies with the cluster.
for i in 1 2 3; do kubectl -n foundation-vault get secret openbao-recovery-shares -o jsonpath="{.data.share-$i}" | base64 -d > openbao-share-$i.b64; done
# 3. Escrow the restic password: without it the backups are unreadable the day OpenBao is lost.
bao_admin kv get -mount=secret -field=password backup/restic          # helper below
```

Operator access (`bao_admin`): a token for the ServiceAccount is exchanged for an OpenBao token **inside the pod**, so no
OpenBao token ever crosses your terminal:

```bash
bao_admin() {
  kubectl -n foundation-vault create token openaether-admin --duration=15m \
  | kubectl -n foundation-vault exec -i openbao-0 -- sh -c \
      'BAO_TOKEN="$(bao write -field=token auth/kubernetes/login role=openaether-admin jwt=-)" exec bao "$@"' sh "$@"
}
```

## Runbooks

**PKI intermediate.** The first `openbao-vault-bootstrap` run prints the intermediate's CSR (public data) and generates it once;
read it with `kubectl -n foundation-vault logs job/openbao-vault-bootstrap | sed -n '/BEGIN CERTIFICATE REQUEST/,/END CERTIFICATE REQUEST/p'`
(logs are rotated: `bao_admin write -field=csr pki/intermediate/generate/internal common_name="OpenAether Workload Intermediate CA"`
makes a new one). Sign it offline with the root ([`pki-root-offline-runbook.md`](./pki-root-offline-runbook.md)), then import it.
Until then the `openbao` ClusterIssuer stays NotReady, by design.

**Break-glass root token** (an emergency only; the quorum of recovery-share holders is the control). Each holder decrypts their
share on their own machine: `kubectl -n foundation-vault get secret openbao-recovery-shares -o jsonpath='{.data.share-1}' |
base64 -d | base64 -d | gpg --decrypt` (try each `share-N`: only yours decrypts) prints the share as hex. Then, in the pod:

```bash
kubectl -n foundation-vault exec -it openbao-0 -- sh
export BAO_TOKEN="$(bao write -field=token auth/kubernetes/login role=openbao-breakglass jwt=-)"   # paste `kubectl … create token openbao-breakglass --duration=10m`, Ctrl-D
bao operator generate-root -cancel        # only if an earlier attempt was left half-done
OTP="$(bao operator generate-root -generate-otp)"; bao operator generate-root -init -otp="$OTP"   # prints the nonce
bao operator generate-root -nonce=<nonce>     # each holder in turn, pasting their share at the hidden prompt; the last call prints the encoded token
bao operator generate-root -decode="<encoded token>" -otp="$OTP"     # the root token. Do the work, then: bao token revoke -self
```

(Measured on a cluster: 2 of 3 PGP-decrypted shares, through the `openbao-breakglass` role, produced a root token.)

**Seal key rotation.** The new key becomes `key` and the old one `previous-key`; roll the StatefulSet (one pod at a time, each Ready and
unsealed before the next); drop `previous-key`; roll again. Then redo the Day-0 escrow of the seal key. The entrypoint computes the key ids.

```bash
OLD="$(kubectl -n foundation-vault get secret openbao-seal-key -o jsonpath='{.data.key}')"; NEW="$(head -c 32 /dev/urandom | base64 -w0)"
kubectl -n foundation-vault patch secret openbao-seal-key --type merge -p "{\"data\":{\"key\":\"$NEW\",\"previous-key\":\"$OLD\"}}"
kubectl -n foundation-vault rollout restart statefulset/openbao && kubectl -n foundation-vault rollout status statefulset/openbao --timeout=600s
kubectl -n foundation-vault patch secret openbao-seal-key --type json -p '[{"op":"remove","path":"/data/previous-key"}]'
kubectl -n foundation-vault rollout restart statefulset/openbao && kubectl -n foundation-vault rollout status statefulset/openbao --timeout=600s
```

(Measured on a 3-replica cluster, 2026-10-13, twice: about 70 s per roll, every pod unsealed with the new key, the data stayed readable.)

**Disaster recovery.** The raft snapshot (backup brick) is encrypted by the seal key, so a restore needs the **escrowed key**, and
the recovery shares that apply afterwards are the SOURCE cluster's:

1. Once the namespace exists and before the vault pods start, create `openbao-seal-key` from the escrow
   (`gpg -d openbao-seal-key.gpg > key && kubectl -n foundation-vault create secret generic openbao-seal-key --from-file=key=key`, then
   delete `key`). Suspend the Flux Kustomizations that consume secrets until step 4, so that nothing picks up the throw-away values the
   fresh bootstrap generates. The init Job refuses to invent a key on a cluster that was initialised before.
2. Let `foundation-vault` deploy: `init.sh` initialises an empty cluster.
3. Restore with a break-glass root token of the fresh cluster (its shares are encrypted to the same operators):
   `bao operator raft snapshot restore -force <file>`. The Kubernetes auth config in the snapshot pins no CA, so logins keep working on a
   rebuilt cluster. If standbys do not follow the restored leader, delete `openbao-1` and `openbao-2` (the PVCs stay).
4. Put the source's shares back (delete the fresh `openbao-recovery-shares`, then
   `kubectl create secret generic openbao-recovery-shares --from-file=share-1=openbao-share-1.b64 …`), then resume Flux.

With any other key the data does not come back (tested on a real OpenBao). Not drilled on a cluster.

**Interrupted first run.** A failure inside the root window (between `sys/init` and the hand-over) revokes the root token on the
way out, whatever the cause, and leaves an initialised OpenBao that nobody can configure (the root token is gone by design; the
calls in the window are retried, and the hand-over is proven from the init Job's own identity before the token goes). On a cluster
that holds nothing yet, start over (measured on a cluster, twice):

```bash
kubectl -n flux-system patch kustomization foundation-vault --type merge -p '{"spec":{"suspend":true}}'
kubectl -n foundation-vault delete job openbao-init openbao-vault-bootstrap
kubectl -n foundation-vault scale statefulset openbao --replicas=0   # wait until the pods are gone
kubectl -n foundation-vault delete pvc data-openbao-0 data-openbao-1 data-openbao-2
kubectl -n foundation-vault delete secret openbao-recovery-shares     # or openbao-recovery-dev; keep openbao-seal-key
kubectl -n flux-system patch kustomization foundation-vault --type merge -p '{"spec":{"suspend":false}}'
```

If the cluster already holds data, use the break-glass instead (its role and policy exist from the first seconds of the root window;
a failure *before* the window ends has nothing to fall back on).

**A re-init regenerates every generated secret** (database passwords, Longhorn's passphrase, restic's password…). Anything that already
used the previous values (a database initialised with them, a volume encrypted with the passphrase) no longer matches: do it only on a
cluster that holds nothing yet, as above.

## Alerts and network

`prometheusrule.yaml`: `OpenBaoWorkloadSealed` (critical: the key Secret is missing or wrong), `OpenBaoRaftNoLeader` (critical),
`OpenBaoRaftLagHigh` (warning). `networkpolicy.yaml`: the OpenBao policy admits ESO, cert-manager, the snapshot job, Prometheus,
the raft peers and the two Jobs; each Job has an egress policy of its own (DNS and OpenBao; only `openbao-init` also reaches the
kube-apiserver, to create the Secrets).

## Limits, stated plainly

- **The static key sits next to the data it protects**: a Secret in the cluster (etcd) and the raft PVCs on the node, and every
  snapshot in the backup store is barrier-encrypted by this key alone (its restic password is another Kubernetes-held secret). It
  removes what the former design leaked (unseal shares and root token in logs and in plain Secrets) and gives hands-free restarts; it does
  **not** separate the key from the cluster. Whoever can read Secrets in `foundation-vault` can open the data; so can whoever reads a
  backup and that Secret. Closing this means a key held outside the cluster (a transit seal on another OpenBao, or a KMS); until then
  RBAC on this namespace is part of the security boundary, and "encrypted at rest" depends on the provider's etcd encryption, which this
  repository does not verify.
- **The bootstrap identity is root-equivalent**, and so is the init Job (it can create Secrets, including service-account-token Secrets
  for any ServiceAccount of the namespace). Both are reachable by whoever can create Pods in `foundation-vault`, exec into them, or change
  what Flux applies, i.e. Git write access. That group is the real root of trust.
- **The break-glass depends on Kubernetes auth working** (and on the recovery quorum). A first run that fails before the root window ends,
  a PGP key that `sys/init` rejects, or a bad hand-over end in a re-init, only possible on a cluster that holds nothing yet.
- A development bootstrap can not be upgraded to production; nothing monitors that the seal key has been escrowed.
- The Kubernetes-auth login is only exercised on a cluster; the harness replaces that one call. The 3-replica join, the seal-key
  rotation and the start-over were run on a cluster; the restore with standbys and the other runbooks were not.
- **Nested PKI mounts are forbidden**: never mount `pki/` and `pki/<x>/` (the second fails with HTTP 400, sub-paths 404).

## Tests

`scripts/dev/test-openbao-bootstrap.sh` runs the real entrypoint, `init.sh` and `configure.sh` against an OpenBao container (needs
Docker, gpg, jq, openssl): both modes, root revocation (witnessed by the audit trail), no secret in any output, idempotence,
create-only, what every policy allows and denies, PGP shares and the break-glass, snapshot restore with the right and a wrong key,
seal-key rotation. It runs in CI. On a cluster: `kubectl -n foundation-vault get pods,jobs` (3× openbao Ready, `openbao-init` and
`openbao-vault-bootstrap` Complete).
